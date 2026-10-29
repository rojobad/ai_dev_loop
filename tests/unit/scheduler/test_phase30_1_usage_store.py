"""Phase 30.1 usage schema, payload, and idempotent SQLite contracts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.usage_ingestion import (
    _FactWrite,
    _ObservationWrite,
    merge_usage_snapshot,
)
from ai_dev_loop.scheduler.domain.token_usage import NormalizedUsage, ProviderNativeUsage
from ai_dev_loop.scheduler.domain.usage_facts import (
    MAX_SQLITE_COUNTER,
    UsageFactPayloadV1,
    UsageObservationCountersV1,
    canonical_sha256,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SqliteSchedulerStore,
)

_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
_ATTEMPT = "usage-logical-attempt"
_DIGEST = "ab" * 32
_PATH = "runs/example/codex/usage-observations-v1.json"


def _native(**overrides: int | None) -> ProviderNativeUsage:
    values: dict[str, int | None] = {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 110,
    }
    values.update(overrides)
    return ProviderNativeUsage.model_validate(values)


def _normalized(**overrides: int | None) -> NormalizedUsage:
    values: dict[str, int | None] = {
        "input_total": 100,
        "input_uncached": 40,
        "cache_read": 60,
        "output_total": 10,
        "reasoning_output": 4,
        "total_tokens": 110,
    }
    values.update(overrides)
    return NormalizedUsage.model_validate(values)


def _observation(
    observation_id: str,
    *,
    logical_attempt_id: str = _ATTEMPT,
    native: ProviderNativeUsage | None = None,
    normalized: NormalizedUsage | None = None,
    availability: str = "known",
    complete: bool = True,
) -> _ObservationWrite:
    counters = UsageObservationCountersV1(
        observation_id=observation_id,
        availability=availability,  # type: ignore[arg-type]
        capture_complete=complete,
        native=native or _native(),
        normalized=normalized or _normalized(),
    )
    return _ObservationWrite(
        counters=counters,
        logical_attempt_id=logical_attempt_id,
        run_id="run-usage",
        provider="codex",
        role="implementer",
        stage="initial",
        native_key="turn-1",
        publisher_attempt_id="publisher-1",
        source_dispatch_id="dispatch-1",
        source_relative_path=_PATH,
        source_digest=_DIGEST,
        source_format="codex_jsonl",
        source_event_ordinal=1,
        source_event_offsets=("0",),
        import_origin="ingestion",
        completion_fence_id="fence-1",
        binding_kind="completion_envelope",
        binding_sha256=_DIGEST,
    )


def _fact(
    *observation_ids: str,
    logical_attempt_id: str = _ATTEMPT,
    coverage: str | None = "complete",
    publisher_attempt_id: str = "publisher-1",
) -> _FactWrite:
    payload = UsageFactPayloadV1(
        logical_attempt_id=logical_attempt_id,
        run_id="run-usage",
        provider="codex",
        role="implementer",
        stage="initial",
        terminal_status="completed",
        coverage=coverage,  # type: ignore[arg-type]
        settlement_status="settled",
        configured_model="gpt-5.6",
        observation_ids=observation_ids,
        diagnostic_codes=(),
        import_origin="ingestion",
    )
    return _FactWrite(
        payload=payload,
        source_paths=(_PATH,),
        source_digests=(_DIGEST,),
        publisher_attempt_id=publisher_attempt_id,
        source_dispatch_id="dispatch-1",
        completion_fence_id="fence-1",
        binding_kind="completion_envelope",
        binding_sha256=_DIGEST,
        import_origin="ingestion",
    )


def test_fresh_store_is_usage_schema_21(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    assert SCHEMA_VERSION == 21
    assert not (tmp_path / "artifacts").exists()
    with store.begin_read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 21
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        assert store.schema_supports_usage_statistics(conn) is True
    assert "scheduler_usage_attempt_facts" in names
    assert "scheduler_usage_observations" in names
    assert "scheduler_usage_attempt_facts" in REQUIRED_TABLES
    assert "idx_scheduler_usage_facts_run" in REQUIRED_INDEXES


def test_migration_fault_rolls_back_to_v20_and_retries(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"

    def pause(statement: str) -> None:
        if "scheduler_usage_attempt_facts" in statement:
            raise RuntimeError("pause before usage tables")

    with pytest.raises(RuntimeError, match="pause before usage tables"):
        SqliteSchedulerStore(db, migration_fault_hook=pause)
    with sqlite3.connect(db) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 20
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_attempt_facts" not in tables

    store = SqliteSchedulerStore(db)
    with store.begin_read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 21
        assert store.schema_supports_usage_statistics(conn) is True


def test_readonly_v20_does_not_migrate(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE scheduler_usage_observations")
        conn.execute("DROP TABLE scheduler_usage_attempt_facts")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 21")
        conn.execute("PRAGMA user_version = 20")
    readonly = SqliteSchedulerStore.open_readonly(db)
    with readonly.begin_read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 20
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
        assert readonly.schema_supports_usage_statistics(conn) is False
    assert "scheduler_usage_attempt_facts" not in tables
    with sqlite3.connect(db) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 20


def test_counter_contract_accepts_null_and_rejects_bad_values() -> None:
    accepted = UsageObservationCountersV1(
        observation_id="cd" * 32,
        availability="partial",
        capture_complete=False,
        native=_native(reasoning_output_tokens=None),
        normalized=_normalized(reasoning_output=None),
    )
    assert accepted.native.reasoning_output_tokens is None
    with pytest.raises(ValidationError):
        ProviderNativeUsage.model_validate({"input_tokens": True, "output_tokens": 1})
    with pytest.raises(ValidationError):
        ProviderNativeUsage.model_validate({"input_tokens": "100", "output_tokens": 1})
    with pytest.raises(ValidationError):
        ProviderNativeUsage.model_validate({"input_tokens": 1.5, "output_tokens": 1})
    with pytest.raises(ValidationError):
        ProviderNativeUsage.model_validate({"input_tokens": -1, "output_tokens": 1})
    with pytest.raises(ValueError, match="overflow"):
        UsageObservationCountersV1(
            observation_id="cd" * 32,
            availability="known",
            capture_complete=True,
            native=_native(total_tokens=MAX_SQLITE_COUNTER + 1),
            normalized=_normalized(),
        )


def test_replay_enrichment_and_conflict_are_distinct(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    observation_id = "11" * 32
    partial = _observation(
        observation_id,
        native=_native(reasoning_output_tokens=None),
        normalized=_normalized(reasoning_output=None),
        availability="partial",
        complete=False,
    )
    complete = _observation(observation_id)
    with store.begin_immediate() as conn:
        first = merge_usage_snapshot(
            store,
            conn,
            fact=_fact(observation_id, coverage="partial"),
            observations=(partial,),
            now=_NOW,
        )
    assert first == "inserted"
    with store.begin_immediate() as conn:
        replay = merge_usage_snapshot(
            store,
            conn,
            fact=_fact(observation_id, coverage="partial"),
            observations=(partial,),
            now=_NOW,
        )
        rows = conn.execute("SELECT COUNT(*) FROM scheduler_usage_observations").fetchone()
    assert replay == "replayed"
    assert rows is not None and int(rows[0]) == 1
    with store.begin_immediate() as conn:
        before = store.get_usage_attempt_fact(conn, _ATTEMPT)
        enriched = merge_usage_snapshot(
            store,
            conn,
            fact=_fact(observation_id),
            observations=(complete,),
            now=_NOW,
        )
        after = store.get_usage_attempt_fact(conn, _ATTEMPT)
        stored = store.get_usage_observation(conn, observation_id)
    assert enriched == "enriched"
    assert before is not None and after is not None and stored is not None
    assert str(after["created_at"]) == str(before["created_at"])
    assert str(after["import_origin"]) == "ingestion"
    assert str(after["publisher_attempt_id"]) == "publisher-1"
    assert int(stored["capture_complete"]) == 1
    assert json.loads(str(stored["normalized_payload"]))["reasoning_output"] == 4
    conflict = _observation(
        observation_id,
        native=_native(output_tokens=99, total_tokens=199),
        normalized=_normalized(output_total=99, total_tokens=199),
    )
    with store.begin_immediate() as conn:
        rejected = merge_usage_snapshot(
            store,
            conn,
            fact=_fact(observation_id),
            observations=(conflict,),
            now=_NOW,
        )
        kept = store.get_usage_observation(conn, observation_id)
        fact = store.get_usage_attempt_fact(conn, _ATTEMPT)
    assert rejected == "conflict"
    assert kept is not None and fact is not None
    assert json.loads(str(kept["normalized_payload"]))["output_total"] == 10
    assert "usage_payload_conflict" in json.loads(str(fact["diagnostic_codes"]))
    assert json.loads(str(fact["fact_payload"]))["configured_model"] == "gpt-5.6"
    assert canonical_sha256(json.loads(str(kept["native_payload"]))) == canonical_sha256(
        json.loads(str(kept["native_payload"]))
    )


def test_insert_fault_rolls_back_only_the_failed_fact(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    with store.begin_immediate() as conn:
        merge_usage_snapshot(
            store,
            conn,
            fact=_fact("22" * 32),
            observations=(_observation("22" * 32),),
            now=_NOW,
        )
    store.usage_write_fault = "insert_fact"
    with (
        pytest.raises(sqlite3.OperationalError, match="injected usage write fault"),
        store.begin_immediate() as conn,
    ):
        merge_usage_snapshot(
            store,
            conn,
            fact=_fact(
                "33" * 32, logical_attempt_id="other-attempt", publisher_attempt_id="publisher-2"
            ),
            observations=(_observation("33" * 32, logical_attempt_id="other-attempt"),),
            now=_NOW,
        )
    with store.begin_read() as conn:
        assert store.get_usage_attempt_fact(conn, _ATTEMPT) is not None
        assert store.get_usage_attempt_fact(conn, "other-attempt") is None
        count = conn.execute("SELECT COUNT(*) FROM scheduler_usage_observations").fetchone()
    assert count is not None and int(count[0]) == 1


def test_usage_json_schemas_match_canonical_payloads() -> None:
    fact = UsageFactPayloadV1(
        logical_attempt_id=_ATTEMPT,
        run_id="run-usage",
        provider="cursor",
        role="reviewer",
        stage="review",
        terminal_status="failed",
        coverage=None,
        settlement_status="debt",
        configured_model=None,
        observation_ids=(),
        diagnostic_codes=("usage_source_debt",),
        import_origin="reconciliation",
    )
    observation = UsageObservationCountersV1(
        observation_id="44" * 32,
        availability="known",
        capture_complete=True,
        native=_native(),
        normalized=_normalized(),
    )
    fact_schema = json.loads(
        schema_path("scheduler-usage-fact-v1.json").read_text(encoding="utf-8")
    )
    observation_schema = json.loads(
        schema_path("scheduler-usage-observation-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(fact_schema)
    jsonschema.Draft202012Validator.check_schema(observation_schema)
    jsonschema.validate(json.loads(fact.model_dump_json()), fact_schema)
    jsonschema.validate(json.loads(observation.model_dump_json()), observation_schema)
    rejected = json.loads(observation.model_dump_json())
    rejected["native"]["input_tokens"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(rejected, observation_schema)
