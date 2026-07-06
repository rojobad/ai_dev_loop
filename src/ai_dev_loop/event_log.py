"""Structured orchestrator event logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.redaction import redact_text

EVENT_SCHEMA_VERSION = 1


class EventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def redact_event_detail(detail: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            if key in {"prompt", "prompt_preview", "patch", "stdin", "cursor_fix_prompt"}:
                redacted[key] = "<redacted>"
            elif len(value) > 200:
                redacted[key] = f"<redacted:{len(value)} chars>"
            else:
                redacted[key] = redact_text(value)
        elif isinstance(value, dict):
            redacted[key] = redact_event_detail(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_event_detail(item)
                if isinstance(item, dict)
                else (
                    "<redacted>"
                    if isinstance(item, str) and len(item) > 200
                    else redact_text(item)
                    if isinstance(item, str)
                    else item
                )
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def append_orchestrator_event(
    run_directory: Path,
    *,
    run_id: str,
    component: str,
    event: str,
    level: EventLevel = EventLevel.INFO,
    status: str | None = None,
    iteration: int | None = None,
    artifact_path: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "level": level.value,
        "component": component,
        "event": event,
        "run_id": run_id,
    }
    if status is not None:
        payload["status"] = status
    if iteration is not None:
        payload["iteration"] = iteration
    if artifact_path is not None:
        payload["artifact_path"] = artifact_path
    if detail:
        payload["detail"] = redact_event_detail(detail)

    log_path = run_directory / "logs" / "events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=False) + "\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    set_sensitive_file_mode(log_path)
