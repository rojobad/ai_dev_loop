"""Canonical compatibility evidence captured from the pre-25.7 production writers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.scheduler.application.sequence_materializer import (
    build_sequence_run_context,
    frozen_entry_hash,
)
from ai_dev_loop.scheduler.cursor_attempt_runner import _sdk_adapter
from ai_dev_loop.scheduler.domain.sequence import (
    FROZEN_SEQUENCE_ENTRY_ADAPTER,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import SubmittedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/phase25_7_historical"


def _canonical_bytes(path: Path) -> bytes:
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    return payload[:-1]


def test_pre_change_sdk_context_and_sequence_writers_round_trip_canonically(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    provenance = json.loads((FIXTURE / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_commit"] == "3d1a7a3074e0edfe8ef2f0aca16d4d12d77c3250"
    expected = {item["path"]: item["canonical_sha256"] for item in provenance["fixtures"]}
    canonical = {name: _canonical_bytes(FIXTURE / name) for name in expected}
    assert {
        name: hashlib.sha256(payload).hexdigest() for name, payload in canonical.items()
    } == expected

    standalone = SqliteSchedulerStore.load_state(
        canonical["submitted-state-context-v7.json"].decode()
    )
    materialized_fixture = SqliteSchedulerStore.load_state(
        canonical["submitted-state-context-v8.json"].decode()
    )
    assert isinstance(standalone, SubmittedState)
    assert isinstance(materialized_fixture, SubmittedState)
    assert standalone.context.schema_version == 7
    assert materialized_fixture.context.schema_version == 8
    for state, name in (
        (standalone, "submitted-state-context-v7.json"),
        (materialized_fixture, "submitted-state-context-v8.json"),
    ):
        _, dumped, digest = SqliteSchedulerStore.dump_state(state)
        assert dumped.encode() == canonical[name]
        assert digest == expected[name]
        cursor_payload = state.context.cursor.model_dump(mode="json")
        assert cursor_payload["schema_version"] == 1
        assert "store_backend" not in cursor_payload
        assert "native_store_layout_version" not in cursor_payload

    historical_sdk = standalone.context.cursor
    repository_root = tmp_path.parent / f"{tmp_path.name}-repository"
    repository_root.mkdir()
    adapter = _sdk_adapter(
        {
            "schema_version": 2,
            "conversation_owner_run_id": standalone.run_id,
            "conversation_key": cursor_conversation_key(standalone.run_id),
            "repository_root": str(repository_root),
            "sdk_version": historical_sdk.sdk_version,  # type: ignore[union-attr]
            "cursor_model": historical_sdk.model,
            "sdk_parameters": historical_sdk.parameters,  # type: ignore[union-attr]
            "sdk_credential_ref": historical_sdk.credential_ref,  # type: ignore[union-attr]
            "sdk_setting_sources": historical_sdk.setting_sources,  # type: ignore[union-attr]
            "cursor_sandbox": historical_sdk.sandbox,
        }
    )
    assert adapter._config.store_backend == "jsonl"
    assert adapter._config.store_root.name == "store"
    assert adapter._config.state_root.name == "bridge"

    prepared = SqliteSchedulerStore.load_sequence_state(
        canonical["prepared-sequence-definition-v3.json"].decode()
    )
    assert isinstance(prepared, PreparedSequenceState)
    assert prepared.definition.schema_version == 3
    assert all(entry.schema_version == 3 for entry in prepared.definition.entries)
    _, dumped_prepared, prepared_digest = SqliteSchedulerStore.dump_sequence_state(prepared)
    assert dumped_prepared.encode() == canonical["prepared-sequence-definition-v3.json"]
    assert prepared_digest == expected["prepared-sequence-definition-v3.json"]

    entry = FROZEN_SEQUENCE_ENTRY_ADAPTER.validate_json(canonical["frozen-sequence-entry-v3.json"])
    dumped_entry, entry_digest = SqliteSchedulerStore.dump_sequence_entry(entry)
    assert dumped_entry.encode() == canonical["frozen-sequence-entry-v3.json"]
    assert entry_digest == expected["frozen-sequence-entry-v3.json"]
    assert frozen_entry_hash(prepared.definition.entries[0]) == entry_digest

    materialized = build_sequence_run_context(
        definition=prepared.definition,
        entry=prepared.definition.entries[0],
        entry_hash=entry_digest,
    )
    assert materialized.schema_version == 8
    assert materialized.model_dump(mode="json") == materialized_fixture.context.model_dump(
        mode="json"
    )
