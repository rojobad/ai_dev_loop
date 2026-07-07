"""Unit tests for the SessionStart hook script."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ai_dev_loop.integrations.codex import assets


def _run_hook(
    *,
    hook_script: Path,
    payload: dict[str, object] | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    stdin = "" if payload is None else json.dumps(payload)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(hook_script)],
        input=stdin,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )


@pytest.fixture
def installed_hook(isolated_integrations: Path, isolated_xdg: Path) -> Path:
    destination = isolated_integrations / ".codex" / "hooks" / assets.HOOK_SCRIPT_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(assets.load_hook_script_content(), encoding="utf-8")
    return destination


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_hook_writes_session_record_and_returns_context(
    installed_hook: Path,
    isolated_xdg: Path,
    source: str,
) -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "019abc00-0000-0000-0000-000000000000",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/workspace/project",
        "model": "o4-mini",
        "source": source,
    }
    result = _run_hook(
        hook_script=installed_hook,
        payload=payload,
        env={"XDG_STATE_HOME": str(isolated_xdg / "state")},
    )
    assert result.returncode == 0
    response = json.loads(result.stdout)
    context = response["hookSpecificOutput"]["additionalContext"]
    assert "019abc00-0000-0000-0000-000000000000" in context
    assert "o4-mini" in context
    assert "/workspace/project" in context
    assert "Do not infer another session" in context

    record_path = (
        isolated_xdg / "state" / "ai_dev_loop" / "codex-sessions" / f"{payload['session_id']}.json"
    )
    assert record_path.is_file()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["session_id"] == payload["session_id"]
    assert record["transcript_path"] == payload["transcript_path"]
    assert "auth" not in record


def test_hook_does_not_read_transcript_contents(installed_hook: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "secret-transcript.jsonl"
    transcript.write_text('{"secret":"token-value"}\n', encoding="utf-8")
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "019abc00-1111-2222-3333-444444444444",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "model": "o4-mini",
        "source": "startup",
    }
    before = transcript.read_text(encoding="utf-8")
    result = _run_hook(hook_script=installed_hook, payload=payload)
    assert result.returncode == 0
    assert transcript.read_text(encoding="utf-8") == before
    assert "token-value" not in result.stdout


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"hook_event_name": "SessionStart", "source": "unsupported"},
        {"hook_event_name": "Other", "source": "startup", "session_id": "abc"},
        {"hook_event_name": "SessionStart", "source": "startup"},
        "not-json",
    ],
)
def test_hook_handles_invalid_or_incomplete_input_safely(
    installed_hook: Path,
    payload: object,
) -> None:
    stdin = (
        "" if payload is None else (payload if isinstance(payload, str) else json.dumps(payload))
    )
    result = subprocess.run(
        [sys.executable, str(installed_hook)],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    json.loads(result.stdout)


def test_hook_session_record_permissions(
    installed_hook: Path,
    isolated_xdg: Path,
    permission_test_root: Path,
) -> None:
    state_root = permission_test_root / "state"
    state_root.mkdir()
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "019abc00-aaaa-bbbb-cccc-dddddddddddd",
        "cwd": str(permission_test_root),
        "model": "o4-mini",
        "source": "startup",
    }
    result = _run_hook(
        hook_script=installed_hook,
        payload=payload,
        env={"XDG_STATE_HOME": str(state_root)},
    )
    assert result.returncode == 0
    record_path = state_root / "ai_dev_loop" / "codex-sessions" / f"{payload['session_id']}.json"
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((state_root / "ai_dev_loop").stat().st_mode) == 0o700
    assert stat.S_IMODE((state_root / "ai_dev_loop" / "codex-sessions").stat().st_mode) == 0o700


def test_hook_rejects_unsafe_session_id_for_path_write(
    installed_hook: Path,
    isolated_xdg: Path,
) -> None:
    sessions_dir = isolated_xdg / "state" / "ai_dev_loop" / "codex-sessions"
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "../escape",
        "cwd": "/workspace/project",
        "model": "o4-mini",
        "source": "startup",
    }
    result = _run_hook(
        hook_script=installed_hook,
        payload=payload,
        env={"XDG_STATE_HOME": str(isolated_xdg / "state")},
    )
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert "hookSpecificOutput" not in response
    assert not sessions_dir.exists() or list(sessions_dir.glob("*.json")) == []


def test_hook_rejects_non_uuid_session_id(
    installed_hook: Path,
    isolated_xdg: Path,
) -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc",
        "cwd": "/workspace/project",
        "model": "o4-mini",
        "source": "startup",
    }
    result = _run_hook(
        hook_script=installed_hook,
        payload=payload,
        env={"XDG_STATE_HOME": str(isolated_xdg / "state")},
    )
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert "hookSpecificOutput" not in response
