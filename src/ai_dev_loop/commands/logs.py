"""Read-only logs command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import shorten_session_id


def render_logs(run_id: str, *, component: str | None = None) -> str:
    run_path, state = load_run(run_id)
    if component is not None and component not in {"cursor", "codex", "ai_dev_loop"}:
        raise ValidationError("component must be one of: cursor, codex, ai_dev_loop")

    if component == "cursor":
        log_path = run_path / "cursor"
        if not log_path.exists():
            return "No Cursor logs found.\n"
        return _read_tree_logs(log_path)
    if component == "codex":
        return _render_codex_logs(run_path, state)

    main_log = run_path / "logs" / "ai_dev_loop.log"
    if not main_log.is_file():
        return "No ai_dev_loop log found.\n"
    return main_log.read_text(encoding="utf-8")


def _looks_like_session_id(value: str) -> bool:
    return len(value) == 36 and value.count("-") == 4


def _summarize_codex_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "session_id" and isinstance(value, str):
            summary[key] = shorten_session_id(value)
        elif key == "args" and isinstance(value, list):
            summary[key] = [
                shorten_session_id(item)
                if isinstance(item, str) and _looks_like_session_id(item)
                else item
                for item in value
            ]
        else:
            summary[key] = value
    return summary


def _summarize_review_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_actionable_findings": payload.get("has_actionable_findings"),
        "findings_count": payload.get("findings_count"),
        "highest_severity": payload.get("highest_severity"),
        "tests_status": payload.get("tests_status"),
        "summary": payload.get("summary"),
        "review_markdown": "<redacted>",
        "cursor_fix_prompt": (
            "<redacted>" if payload.get("cursor_fix_prompt") is not None else None
        ),
    }


def _render_codex_logs(run_path: Path, state) -> str:  # type: ignore[no-untyped-def]
    codex_root = run_path / "codex"
    if not codex_root.exists():
        return "No Codex logs found.\n"

    chunks: list[str] = []
    if state.iterations:
        for entry in sorted(state.iterations, key=lambda item: item.get("number", 0)):
            number = entry.get("number")
            review = entry.get("review")
            if isinstance(review, dict):
                chunks.append(f"=== review {number} summary ===\n")
                chunks.append(json.dumps(review, indent=2) + "\n")

            codex = entry.get("codex")
            if isinstance(codex, dict):
                chunks.append(f"=== codex {number} artifact paths ===\n")
                for key in sorted(codex):
                    value = codex[key]
                    if not isinstance(value, (str, int)):
                        continue
                    chunks.append(f"  {key}: {value}\n")
                    if key.endswith("_path") and isinstance(value, str):
                        artifact = run_path / value
                        if artifact.is_file():
                            chunks.append(f"    size: {artifact.stat().st_size} bytes\n")
                chunks.append("\n")

    for path in sorted(codex_root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(run_path))
        if path.suffix == ".jsonl":
            line_count = sum(1 for _ in path.open(encoding="utf-8"))
            chunks.append(f"=== {rel} ===\n")
            chunks.append(f"jsonl events: {line_count} lines (content redacted)\n")
            continue
        if path.name.endswith(".metadata.json"):
            chunks.append(f"=== {rel} ===\n")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    chunks.append(json.dumps(_summarize_codex_metadata(payload), indent=2) + "\n")
                else:
                    chunks.append("<metadata redacted>\n")
            except json.JSONDecodeError:
                chunks.append("<invalid metadata json>\n")
            continue
        if path.parent.name == "reviews" and path.suffix == ".json":
            chunks.append(f"=== {rel} ===\n")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    chunks.append(json.dumps(_summarize_review_result(payload), indent=2) + "\n")
                else:
                    chunks.append("<review result redacted>\n")
            except json.JSONDecodeError:
                chunks.append("<invalid review json>\n")
            continue
        if path.suffix == ".md":
            chunks.append(f"=== {rel} ===\n")
            chunks.append(f"markdown report: {path.stat().st_size} bytes (content redacted)\n")
            continue
        if path.suffix == ".txt":
            chunks.append(f"=== {rel} ===\n")
            chunks.append(f"stderr: {path.stat().st_size} bytes (content redacted)\n")

    return "".join(chunks) if chunks else "No Codex log files found.\n"


def _read_tree_logs(root) -> str:  # type: ignore[no-untyped-def]
    chunks: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(f"=== {path.name} ===\n")
            chunks.append(path.read_text(encoding="utf-8"))
            if not chunks[-1].endswith("\n"):
                chunks.append("\n")
    return "".join(chunks) if chunks else "No log files found.\n"
