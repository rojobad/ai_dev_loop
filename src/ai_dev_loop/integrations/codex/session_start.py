#!/usr/bin/env python3
"""Codex SessionStart hook for ai_dev_loop."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
VALID_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
HOOK_EVENT_NAME = "SessionStart"
DIR_MODE = 0o700
SENSITIVE_FILE_MODE = 0o600
SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "ai_dev_loop"
    return Path.home() / ".local" / "state" / "ai_dev_loop"


def codex_sessions_dir() -> Path:
    return state_dir() / "codex-sessions"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        return
    root = state_dir().resolve()
    target = path.resolve()
    if not target.is_relative_to(root):
        os.chmod(target, DIR_MODE)
        return
    current = root
    if current.is_dir():
        os.chmod(current, DIR_MODE)
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_dir():
            os.chmod(current, DIR_MODE)


def is_safe_session_id(value: str) -> bool:
    return bool(SESSION_ID_RE.fullmatch(value))


def set_sensitive_file_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, SENSITIVE_FILE_MODE)


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        set_sensitive_file_mode(path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def build_additional_context(session_id: str, model: str | None, cwd: str | None) -> str:
    lines = [
        "ai_dev_loop integration is active.",
        f"Current Codex session ID: {session_id}",
        f"Current Codex model: {model or 'unknown'}",
        f"Current session working directory: {cwd or 'unknown'}",
        (
            "When preparing an approved ai_dev_loop run, pass this exact session ID. "
            "Do not infer another session and do not use --last."
        ),
    ]
    return "\n".join(lines)


def safe_response(additional_context: str | None = None) -> dict[str, object]:
    if additional_context:
        return {"hookSpecificOutput": {"additionalContext": additional_context}}
    return {}


def write_session_record(
    *,
    session_id: str,
    model: str | None,
    cwd: str | None,
    transcript_path: str | None,
    source: str | None,
) -> None:
    ensure_dir(codex_sessions_dir())
    record = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "model": model,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "source": source,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    atomic_write_json(codex_sessions_dir() / f"{session_id}.json", record)


def handle_payload(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("hook_event_name") != HOOK_EVENT_NAME:
        return safe_response()

    source = payload.get("source")
    if not isinstance(source, str) or source not in VALID_SOURCES:
        return safe_response()

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        return safe_response()
    session_id = session_id.strip()
    if not session_id or not is_safe_session_id(session_id):
        return safe_response()

    model = payload.get("model")
    cwd = payload.get("cwd")
    transcript_path = payload.get("transcript_path")

    write_session_record(
        session_id=session_id,
        model=model if isinstance(model, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        transcript_path=transcript_path if isinstance(transcript_path, str) else None,
        source=source,
    )
    return safe_response(
        build_additional_context(
            session_id,
            model if isinstance(model, str) else None,
            cwd if isinstance(cwd, str) else None,
        )
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps(safe_response()))
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print(json.dumps(safe_response()))
            return 0
        response = handle_payload(payload)
        print(json.dumps(response))
        return 0
    except Exception:
        print(json.dumps(safe_response()))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
