"""Success and failure envelope builders."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from ai_dev_loop.integration_api.clock import observed_at_now
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.models import (
    ApiVersion,
    IntegrationEnvelope,
    current_api_version,
)
from ai_dev_loop.integration_api.validation import validate_envelope_wire_payload


def _format_observed_at(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    utc = value.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0").rstrip(".")
    return f"{text}Z"


def build_success_envelope(data: Any, *, observed_at: datetime | None = None) -> dict[str, Any]:
    when = observed_at or observed_at_now()
    envelope = IntegrationEnvelope.model_validate(
        {
            "apiVersion": current_api_version().model_dump(by_alias=True),
            "ok": True,
            "observedAt": when,
            "data": data,
            "error": None,
        }
    )
    payload = envelope.model_dump(by_alias=True, mode="json")
    payload["observedAt"] = _format_observed_at(when)
    validate_envelope_wire_payload(payload)
    return payload


def build_failure_envelope(
    code: IntegrationErrorCode,
    message: str,
    *,
    observed_at: datetime | None = None,
    api_version: ApiVersion | None = None,
) -> dict[str, Any]:
    when = observed_at or observed_at_now()
    version = api_version or current_api_version()
    envelope = IntegrationEnvelope.model_validate(
        {
            "apiVersion": version.model_dump(by_alias=True),
            "ok": False,
            "observedAt": when,
            "data": None,
            "error": {"code": code.value, "message": message},
        }
    )
    payload = envelope.model_dump(by_alias=True, mode="json")
    payload["observedAt"] = _format_observed_at(when)
    validate_envelope_wire_payload(payload)
    return payload


def write_envelope(payload: dict[str, Any], *, stream: Any | None = None) -> None:
    target = stream if stream is not None else sys.stdout
    target.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    target.write("\n")
    target.flush()


def emit_success(data: Any, *, stream: Any | None = None) -> None:
    write_envelope(build_success_envelope(data), stream=stream)


def emit_failure(error: IntegrationApiError, *, stream: Any | None = None) -> None:
    write_envelope(
        build_failure_envelope(error.code, error.message),
        stream=stream,
    )
