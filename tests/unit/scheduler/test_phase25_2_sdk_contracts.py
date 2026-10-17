"""Independent W-01/W-03/W-04 contracts for Phase 25.2."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from referencing import Registry, Resource

from ai_dev_loop.dashboard.application.ports import AttemptObservation, RunObservation
from ai_dev_loop.dashboard.domain.metrics import first_execution_launches
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.sequence import SequenceManifest
from ai_dev_loop.scheduler.domain.state import CursorSdkRuntimeBinding, ImplementerBinding


def _validate_schema(payload: object, name: str) -> None:
    schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
    resources = []
    for path in schema_path(name).parent.glob("*.json"):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in candidate:
            resources.append((candidate["$id"], Resource.from_contents(candidate)))
    jsonschema.Draft202012Validator(
        schema,
        registry=Registry().with_resources(resources),
    ).validate(payload)


def _manifest() -> dict[str, object]:
    phase = {
        "name": "sdk-phase",
        "short_name": "SDK phase",
        "plan_path": "docs/plan.md",
        "prompt_source_path": "docs/prompt.txt",
        "cursor": {
            "runtime": "sdk-local",
            "model": "grok-4.7",
            "parameters": {
                "reasoning_effort": "high",
                "context": "256k",
                "fast": False,
            },
        },
        "codex": {"review_model": "gpt-6.1-sol", "review_reasoning_effort": "high"},
    }
    first = {**phase, "commit_message": "accepted phase", "implementer": {"provider": "cursor"}}
    second = {
        **phase,
        "name": "codex-phase",
        "short_name": "Codex phase",
        "implementer": {
            "provider": "codex",
            "command": "codex",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
            "timeout_minutes": 90,
        },
    }
    return {
        "schema_version": 4,
        "name": "mixed-sdk-sequence",
        "short_name": "Mixed SDK",
        "phases": [first, second],
    }


def test_manifest_v4_requires_names_and_complete_atomic_sdk_selection() -> None:
    manifest = SequenceManifest.model_validate(_manifest())
    _validate_schema(manifest.model_dump(mode="json"), "scheduler-sequence-manifest-v4.json")
    assert manifest.schema_version == 4
    assert manifest.phases[0].cursor is not None
    assert list(manifest.phases[0].cursor.parameters or {}) == [
        "context",
        "fast",
        "reasoning_effort",
    ]

    missing = _manifest()
    del missing["short_name"]  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="short_name"):
        SequenceManifest.model_validate(missing)

    partial = _manifest()
    partial["phases"][0]["cursor"].pop("parameters")  # type: ignore[index,union-attr]
    with pytest.raises(ValidationError, match="parameters"):
        SequenceManifest.model_validate(partial)


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_historical_manifest_versions_reject_sdk_only_fields(schema_version: int) -> None:
    payload = _manifest()
    payload["schema_version"] = schema_version
    phases = payload["phases"]  # type: ignore[assignment]
    if schema_version < 3:
        payload.pop("short_name")
        for phase in phases:  # type: ignore[union-attr]
            phase.pop("short_name")
    if schema_version == 1:
        for phase in phases:  # type: ignore[union-attr]
            phase.pop("implementer")

    with pytest.raises(ValidationError, match="runtime and parameters require manifest v4"):
        SequenceManifest.model_validate(payload)


def test_named_manifest_v3_keeps_historical_cli_cursor_overrides() -> None:
    payload = _manifest()
    payload["schema_version"] = 3
    for phase in payload["phases"]:  # type: ignore[union-attr]
        phase["cursor"] = {
            "command": "agent",
            "model": "composer-2.5-fast",
            "output_format": "stream-json",
        }

    manifest = SequenceManifest.model_validate(payload)

    assert manifest.schema_version == 3
    assert manifest.phases[0].cursor is not None
    assert manifest.phases[0].cursor.runtime is None


def test_sdk_implementer_binding_has_fresh_shape_and_no_cli_command() -> None:
    binding = ImplementerBinding(
        schema_version=2,
        provider="cursor",
        runtime="sdk-local",
        command=None,
        model="grok-4.7",
        timeout_minutes=90,
        sandbox="enabled",
    )
    payload = binding.model_dump(mode="json")
    assert payload["schema_version"] == 2
    assert payload["runtime"] == "sdk-local"
    assert "command" not in payload


@pytest.mark.parametrize(
    ("agent_id", "native_run_id"),
    [
        ("not-a-cursor-agent", "run-019abc00-1111-4222-8333-555555555555"),
        ("019abc00-1111-4222-8333-444444444444", "019abc00-1111-4222-8333-555555555555"),
    ],
)
def test_sdk_runtime_identity_rejects_malformed_agent_and_native_run_ids(
    agent_id: str,
    native_run_id: str,
) -> None:
    with pytest.raises(ValidationError):
        CursorSdkRuntimeBinding(
            conversation_owner_run_id="project-sdk-run",
            conversation_key="a" * 64,
            agent_id=agent_id,
            native_run_id=native_run_id,
            identity_artifact_path="cursor/sdk-identity.json",
            identity_artifact_sha256="b" * 64,
            latest_journal_path="cursor/sdk-journal.jsonl",
            latest_journal_sha256="c" * 64,
        )


def test_genuine_historical_fixtures_keep_recorded_bytes_and_parse() -> None:
    root = Path(__file__).resolve().parents[3]
    provenance = json.loads(
        (root / "tests/fixtures/phase25_historical/provenance.json").read_text(encoding="utf-8")
    )
    from ai_dev_loop.scheduler.domain.sequence import PREPARED_SEQUENCE_STATE_ADAPTER

    for fixture in provenance["fixtures"]:
        path = root / fixture["path"]
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == fixture["sha256"]
        if path.name == "prepared.json":
            PREPARED_SEQUENCE_STATE_ADAPTER.validate_json(payload)


def test_dashboard_classifies_sdk_effects_by_stable_scheduler_meaning() -> None:
    instant = datetime(2026, 10, 17, 12, tzinfo=UTC)
    run = RunObservation("sdk-run", "project", "cursor", "cursor_ready", instant, None)
    create = AttemptObservation(
        "sdk-run",
        "project",
        "cursor",
        "cursor.create_chat",
        1,
        "completed",
        instant,
        instant,
        True,
    )
    turn = AttemptObservation(
        "sdk-run",
        "project",
        "cursor",
        "cursor.run_turn",
        1,
        "completed",
        instant,
        instant,
        True,
    )
    launches, coverage = first_execution_launches((run,), (create, turn))
    assert launches == {"sdk-run": instant}
    assert coverage.unknown_execution_kinds == 0
