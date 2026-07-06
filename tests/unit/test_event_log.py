"""Unit tests for structured orchestrator event logging."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.event_log import append_orchestrator_event, redact_event_detail


def test_append_orchestrator_event_writes_jsonl(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    append_orchestrator_event(
        run_directory,
        run_id="fixture-project-20260704T134512Z-abc123",
        component="orchestrator",
        event="prepare_completed",
        status="prepared",
        detail={"project": "fixture-project"},
    )
    events_path = run_directory / "logs" / "events.jsonl"
    assert events_path.is_file()
    payload = json.loads(events_path.read_text(encoding="utf-8").strip())
    assert payload["schema_version"] == 1
    assert payload["component"] == "orchestrator"
    assert payload["event"] == "prepare_completed"
    assert payload["run_id"] == "fixture-project-20260704T134512Z-abc123"
    assert payload["status"] == "prepared"


def test_redact_event_detail_hides_prompt_like_fields() -> None:
    redacted = redact_event_detail(
        {
            "prompt": "Implement the entire plan with secret token=abc123",
            "patch": "diff --git a/file b/file",
            "message": "short message",
        }
    )
    assert redacted["prompt"] == "<redacted>"
    assert redacted["patch"] == "<redacted>"
    assert redacted["message"] == "short message"
