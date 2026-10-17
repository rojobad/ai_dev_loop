"""Phase 28.1 short-name domain and manifest contracts."""

from __future__ import annotations

import json

import jsonschema
import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.presentation import legacy_short_name, require_short_name
from ai_dev_loop.scheduler.domain.sequence import SequenceManifest


def _phase(name: str, short_name: object = "Leaf") -> dict[str, object]:
    return {
        "name": name,
        "short_name": short_name,
        "plan_path": "docs/plans/sample-plan.md",
        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
        "codex": {
            "review_model": "gpt-6.1-sol",
            "review_reasoning_effort": "high",
        },
    }


def _manifest(short_name: object = "Sequence") -> dict[str, object]:
    first = _phase("one", "One")
    first["commit_message"] = "accepted checkpoint"
    return {
        "schema_version": 3,
        "name": "Long authoritative sequence name",
        "short_name": short_name,
        "phases": [first, _phase("two", "Two")],
    }


def test_short_name_uses_trimmed_unicode_code_points() -> None:
    multibyte = "界" * 35
    assert require_short_name(f"  {multibyte}  ") == multibyte
    assert len(require_short_name(multibyte)) == 35
    with pytest.raises(ValidationError, match="35 Unicode code points"):
        require_short_name(multibyte + "界")


@pytest.mark.parametrize(
    "value",
    [None, True, 12, "", "   ", "bad\nname", "bad\x00name", "\nname", "name\t"],
)
def test_short_name_rejects_null_non_string_blank_and_control(value: object) -> None:
    with pytest.raises(ValidationError):
        require_short_name(value)


def test_legacy_projection_is_bounded_and_has_id_fallback() -> None:
    assert legacy_short_name("  " + "é" * 40 + "  ", fallback_id="fallback") == "é" * 35
    assert legacy_short_name("\n", fallback_id="run-123") == "run-123"
    assert legacy_short_name(None, fallback_id="x" * 40) == "x" * 35


def test_manifest_v3_requires_all_names_but_freezes_v2_implementer_semantics() -> None:
    parsed = SequenceManifest.model_validate(_manifest())
    assert parsed.schema_version == 3
    assert parsed.short_name == "Sequence"
    assert [phase.short_name for phase in parsed.phases] == ["One", "Two"]

    missing_sequence = _manifest()
    del missing_sequence["short_name"]
    with pytest.raises(PydanticValidationError, match="requires sequence short_name"):
        SequenceManifest.model_validate(missing_sequence)

    missing_leaf = _manifest()
    del missing_leaf["phases"][0]["short_name"]  # type: ignore[index]
    with pytest.raises(PydanticValidationError, match="every phase"):
        SequenceManifest.model_validate(missing_leaf)


def test_manifest_v3_json_schema_matches_domain_boundary() -> None:
    schema = json.loads(schema_path("scheduler-sequence-manifest-v3.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(_manifest())
    jsonschema.Draft202012Validator(schema).validate(_manifest("  " + "界" * 35 + "  "))
    invalid = _manifest("x" * 36)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid)


def test_legacy_manifest_rejects_short_name_fields() -> None:
    payload = _manifest()
    payload["schema_version"] = 2
    with pytest.raises(PydanticValidationError, match="short_name requires manifest v3"):
        SequenceManifest.model_validate(payload)
    payload["short_name"] = None
    for phase in payload["phases"]:  # type: ignore[union-attr]
        phase["short_name"] = None  # type: ignore[index]
    with pytest.raises(PydanticValidationError, match="short_name requires manifest v3"):
        SequenceManifest.model_validate(payload)
