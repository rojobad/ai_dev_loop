"""Local validation for JSON Schemas sent to Codex as response_format / output-schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError

# Keywords known to be rejected by current Codex response_format backends.
_INCOMPATIBLE_RESPONSE_FORMAT_KEYWORDS = frozenset({"uniqueItems"})

# String fields inspected on a single recognized API error object for uniqueItems.
_API_ERROR_TEXT_FIELDS = frozenset({"message", "param", "detail", "details"})

# Codex exec --json wrappers that may carry a JSON-serialized API error envelope.
_RECOGNIZED_FAILURE_WRAPPER_TYPES = frozenset({"error", "turn.failed"})


def find_incompatible_response_format_keywords(
    node: Any,
    *,
    path: str = "$",
) -> list[tuple[str, str]]:
    """Return (json_path, keyword) pairs for incompatible response-format keywords."""

    findings: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path != "$" else f"$.{key}"
            if key in _INCOMPATIBLE_RESPONSE_FORMAT_KEYWORDS:
                findings.append((child_path, key))
            findings.extend(find_incompatible_response_format_keywords(value, path=child_path))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            findings.extend(
                find_incompatible_response_format_keywords(item, path=f"{path}[{index}]")
            )
    return findings


def validate_codex_response_schema(schema_file: Path, *, schema_name: str | None = None) -> None:
    """Reject schemas that Codex response_format backends are known to reject.

    Operates on parsed JSON only. Does not call models or the network.
    """

    name = schema_name or schema_file.name
    if not schema_file.is_file():
        raise ValidationError(f"Codex response schema missing: {name}")
    try:
        payload = json.loads(schema_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Codex response schema is not valid JSON: {name}: {exc}") from exc
    findings = find_incompatible_response_format_keywords(payload)
    if not findings:
        return
    rendered = ", ".join(f"{keyword} at {path}" for path, keyword in findings)
    raise ValidationError(
        f"Codex response schema {name} contains incompatible keyword(s): {rendered}"
    )


def events_text_indicates_usage_limit_exceeded(text: str) -> bool:
    """Return True when one recognized API error object has code ``usage_limit_exceeded``.

    Supports the Codex exec ``--json`` transport where a recognized wrapper may store a
    JSON-serialized API error envelope in ``message``. Does not infer from prose, exit
    status, ``rate_limit_exceeded``, stderr, or tokens split across lines.
    """

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for error in _recognized_api_error_objects(payload):
            if error.get("code") == "usage_limit_exceeded":
                return True
    return False


def events_indicate_usage_limit_exceeded(events_path: Path) -> bool:
    """File-based helper for Codex review JSONL usage-limit classification."""

    if not events_path.is_file():
        return False
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return events_text_indicates_usage_limit_exceeded(text)


def events_indicate_adjudication_schema_rejection(events_path: Path) -> bool:
    """Return True only when one structured API error object carries both facts.

    Supports the Codex exec ``--json`` transport where a recognized wrapper
    (``type=error`` / ``turn.failed``) may store a JSON-serialized API error
    envelope in ``message``. Requires ``invalid_json_schema`` and ``uniqueItems``
    inside that same parsed API error object. Does not aggregate tokens across
    lines and does not treat free-form agent/message content as evidence.
    """

    if not events_path.is_file():
        return False
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for error in _recognized_api_error_objects(payload):
            if _api_error_is_unique_items_schema_rejection(error):
                return True
    return False


def _recognized_api_error_objects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only fail-closed, recognized Codex/OpenAI-style API error objects."""

    errors: list[dict[str, Any]] = []
    top_type = payload.get("type")
    nested = payload.get("error")

    # Direct nested API error object (non-transport / bare envelope).
    if (
        isinstance(nested, dict)
        and _looks_like_api_error_object(nested)
        and top_type in {None, "error"}
    ):
        errors.append(nested)

    # Real Codex exec --json wrappers: JSON-serialized API envelope in message.
    if top_type in _RECOGNIZED_FAILURE_WRAPPER_TYPES:
        for message in _wrapper_message_candidates(payload):
            errors.extend(_api_errors_from_serialized_envelope(message))
    return errors


def _wrapper_message_candidates(payload: dict[str, Any]) -> list[str]:
    """Return allowlisted message strings from recognized failure wrappers only."""

    top_type = payload.get("type")
    candidates: list[str] = []
    if top_type == "error":
        message = payload.get("message")
        if isinstance(message, str):
            candidates.append(message)
    elif top_type == "turn.failed":
        nested = payload.get("error")
        if isinstance(nested, dict):
            message = nested.get("message")
            if isinstance(message, str):
                candidates.append(message)
    return candidates


def _api_errors_from_serialized_envelope(message: str) -> list[dict[str, Any]]:
    """Parse a wrapper message only when it is a JSON object API error envelope."""

    text = message.strip()
    # Fail closed: free-form prose never starts a JSON object.
    if not text.startswith("{"):
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []

    nested = parsed.get("error")
    if isinstance(nested, dict) and _looks_like_api_error_object(nested):
        return [nested]
    # Bare API error object must carry an explicit string code.
    if isinstance(parsed.get("code"), str):
        return [parsed]
    return []


def _looks_like_api_error_object(error: dict[str, Any]) -> bool:
    code = error.get("code")
    err_type = error.get("type")
    return isinstance(code, str) or isinstance(err_type, str)


def _api_error_is_unique_items_schema_rejection(error: dict[str, Any]) -> bool:
    """True when one API error object encodes invalid_json_schema and uniqueItems."""

    code = error.get("code")
    err_type = error.get("type")
    has_invalid_schema = code == "invalid_json_schema" or err_type == "invalid_json_schema"
    if not has_invalid_schema:
        return False
    return _error_text_mentions_unique_items(error)


def _error_text_mentions_unique_items(error: dict[str, Any]) -> bool:
    """Inspect only allowlisted string fields on the error object itself."""

    for key in _API_ERROR_TEXT_FIELDS:
        value = error.get(key)
        if isinstance(value, str) and "uniqueItems" in value:
            return True
        # Some APIs return details as a list of strings; accept only that shallow shape.
        if key == "details" and isinstance(value, list):
            for item in value:
                if isinstance(item, str) and "uniqueItems" in item:
                    return True
    return False
