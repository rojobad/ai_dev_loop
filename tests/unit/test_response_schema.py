"""Unit tests for Codex response_format schema compatibility checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import schema_path
from ai_dev_loop.response_schema import (
    events_indicate_adjudication_schema_rejection,
    find_incompatible_response_format_keywords,
    validate_codex_response_schema,
)


def test_github_pr_review_schema_has_no_unique_items() -> None:
    path = schema_path("github-pr-review-result-v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert find_incompatible_response_format_keywords(payload) == []
    validate_codex_response_schema(path, schema_name="github-pr-review-result-v1.json")


def test_validator_rejects_unique_items_fixture(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "eligible_thread_ids": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="uniqueItems"):
        validate_codex_response_schema(bad, schema_name="bad.json")


def _api_envelope(*, code: str, message: str) -> dict:
    return {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": code,
            "message": message,
            "param": "text.format.schema",
        },
        "status": 400,
    }


def _codex_error_wrapper(envelope: dict) -> dict:
    """Real Codex exec --json transport: API envelope serialized in message."""

    return {"type": "error", "message": json.dumps(envelope, separators=(",", ":"))}


def test_events_indicate_schema_rejection_real_codex_transport(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    envelope = _api_envelope(
        code="invalid_json_schema",
        message=(
            "Invalid schema for response_format 'codex_output_schema': "
            "In context=('properties', 'eligible_thread_ids'), "
            "'uniqueItems' is not supported."
        ),
    )
    events.write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps(_codex_error_wrapper(envelope)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is True


def test_anonymized_fixture_matches_real_codex_transport() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "codex_adjudication_schema_rejection_events.jsonl"
    )
    assert fixture.is_file()
    assert events_indicate_adjudication_schema_rejection(fixture) is True


def test_turn_failed_wrapper_with_serialized_envelope(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    envelope = _api_envelope(
        code="invalid_json_schema",
        message="schema keyword uniqueItems is not supported",
    )
    events.write_text(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": json.dumps(envelope, separators=(",", ":"))},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is True


def test_events_without_schema_rejection_are_false(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            _codex_error_wrapper(_api_envelope(code="rate_limit_exceeded", message="slow down"))
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False


def test_split_tokens_across_events_fail_closed(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    _codex_error_wrapper(
                        _api_envelope(
                            code="invalid_json_schema",
                            message="schema rejected",
                        )
                    )
                ),
                json.dumps(
                    _codex_error_wrapper(
                        _api_envelope(
                            code="invalid_request_error",
                            message="uniqueItems is unsupported",
                        )
                    )
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False


def test_freeform_wrapper_message_is_not_schema_rejection(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "error",
                "message": (
                    "API said invalid_json_schema because uniqueItems broke response_format"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False


def test_freeform_agent_content_is_not_schema_rejection(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "agent_message",
                "content": (
                    "API said invalid_json_schema because uniqueItems broke response_format"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False


def test_serialized_envelope_without_unique_items_fails_closed(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            _codex_error_wrapper(
                _api_envelope(
                    code="invalid_json_schema",
                    message="schema rejected for unrelated reasons",
                )
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False


def test_serialized_non_error_json_object_fails_closed(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "error",
                "message": json.dumps(
                    {
                        "note": "invalid_json_schema and uniqueItems mentioned freely",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert events_indicate_adjudication_schema_rejection(events) is False
