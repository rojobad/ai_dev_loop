"""Unit tests for hooks.json merge and uninstall logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex import hooks_json


def test_merge_into_empty_document(tmp_path: Path) -> None:
    hook_script = tmp_path / "ai_dev_loop_session_start.py"
    hook_script.write_text("hook", encoding="utf-8")
    merged, changed = hooks_json.merge_hook_registration({}, hook_script=hook_script)
    assert changed is True
    command = hooks_json.build_hook_command(hook_script)
    status = hooks_json.registration_status(merged, expected_command=command)
    assert status.present is True
    assert status.duplicate_count == 0


def test_merge_preserves_unrelated_hooks(tmp_path: Path) -> None:
    hook_script = tmp_path / "ai_dev_loop_session_start.py"
    hook_script.write_text("hook", encoding="utf-8")
    document = {
        "version": 1,
        "hooks": {
            "PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "echo hi"}]}],
            "SessionStart": [
                {
                    "matcher": "other",
                    "hooks": [{"type": "command", "command": "echo other"}],
                }
            ],
        },
    }
    merged, changed = hooks_json.merge_hook_registration(document, hook_script=hook_script)
    assert changed is True
    assert merged["version"] == 1
    assert "PreToolUse" in merged["hooks"]
    matchers = [entry["matcher"] for entry in merged["hooks"]["SessionStart"]]
    assert "other" in matchers
    assert hooks_json.HOOK_MATCHER in matchers


def test_merge_is_idempotent_for_matching_hook(tmp_path: Path) -> None:
    hook_script = tmp_path / "ai_dev_loop_session_start.py"
    hook_script.write_text("hook", encoding="utf-8")
    first, changed_first = hooks_json.merge_hook_registration({}, hook_script=hook_script)
    assert changed_first is True
    second, changed_second = hooks_json.merge_hook_registration(first, hook_script=hook_script)
    assert changed_second is False
    assert second == first


def test_merge_updates_changed_hook_command(tmp_path: Path) -> None:
    new_script = tmp_path / ".codex" / "hooks" / "ai_dev_loop_session_start.py"
    new_script.parent.mkdir(parents=True)
    new_script.write_text("new", encoding="utf-8")
    stale_command = "python3 /old/home/.codex/hooks/ai_dev_loop_session_start.py"
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": hooks_json.HOOK_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": stale_command,
                            "statusMessage": hooks_json.HOOK_STATUS_MESSAGE,
                        }
                    ],
                }
            ]
        }
    }
    updated, changed = hooks_json.merge_hook_registration(document, hook_script=new_script)
    assert changed is True
    new_command = hooks_json.build_hook_command(new_script)
    status = hooks_json.registration_status(updated, expected_command=new_command)
    assert status.present is True
    assert stale_command not in json.dumps(updated)
    assert updated["hooks"]["SessionStart"][0]["hooks"][0]["command"] == new_command


def test_merge_removes_duplicate_registrations(tmp_path: Path) -> None:
    hook_script = tmp_path / "ai_dev_loop_session_start.py"
    hook_script.write_text("hook", encoding="utf-8")
    command = hooks_json.build_hook_command(hook_script)
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": hooks_json.HOOK_MATCHER,
                    "hooks": [
                        {"type": "command", "command": command},
                        {"type": "command", "command": command},
                    ],
                }
            ]
        }
    }
    merged, changed = hooks_json.merge_hook_registration(document, hook_script=hook_script)
    assert changed is True
    status = hooks_json.registration_status(merged, expected_command=command)
    assert status.present is True
    assert status.duplicate_count == 0


def test_load_invalid_hooks_json_fails(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid hooks.json"):
        hooks_json.load_hooks_document(path)


def test_uninstall_preserves_unrelated_hooks(tmp_path: Path) -> None:
    hook_script = tmp_path / "ai_dev_loop_session_start.py"
    hook_script.write_text("hook", encoding="utf-8")
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "other",
                    "hooks": [{"type": "command", "command": "echo keep"}],
                },
                {
                    "matcher": hooks_json.HOOK_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": hooks_json.build_hook_command(hook_script),
                        }
                    ],
                },
            ]
        }
    }
    updated, removed = hooks_json.remove_all_ai_dev_loop_hooks(document, hook_script=hook_script)
    assert removed == 1
    assert len(updated["hooks"]["SessionStart"]) == 1
    assert updated["hooks"]["SessionStart"][0]["matcher"] == "other"


def test_uninstall_preserves_unrelated_same_named_script(tmp_path: Path) -> None:
    hook_script = tmp_path / "installed" / "ai_dev_loop_session_start.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("installed", encoding="utf-8")
    other_script = tmp_path / "other" / "ai_dev_loop_session_start.py"
    other_script.parent.mkdir(parents=True)
    other_script.write_text("other", encoding="utf-8")
    our_command = hooks_json.build_hook_command(hook_script)
    other_command = hooks_json.build_hook_command(other_script)
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": hooks_json.HOOK_MATCHER,
                    "hooks": [
                        {"type": "command", "command": other_command},
                        {"type": "command", "command": our_command},
                    ],
                }
            ]
        }
    }
    updated, removed = hooks_json.remove_all_ai_dev_loop_hooks(document, hook_script=hook_script)
    assert removed == 1
    remaining_commands = [
        hook["command"] for entry in updated["hooks"]["SessionStart"] for hook in entry["hooks"]
    ]
    assert other_command in remaining_commands
    assert our_command not in remaining_commands


def test_merge_preserves_unrelated_same_named_canonical_hook(tmp_path: Path) -> None:
    hook_script = tmp_path / ".codex" / "hooks" / "ai_dev_loop_session_start.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("installed", encoding="utf-8")
    unrelated_command = "python3 /custom/.codex/hooks/ai_dev_loop_session_start.py"
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": hooks_json.HOOK_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": unrelated_command,
                            "statusMessage": "Other hook",
                        }
                    ],
                }
            ]
        }
    }
    merged, changed = hooks_json.merge_hook_registration(document, hook_script=hook_script)
    assert changed is True
    remaining_commands = [
        hook["command"] for entry in merged["hooks"]["SessionStart"] for hook in entry["hooks"]
    ]
    assert unrelated_command in remaining_commands
    assert hooks_json.build_hook_command(hook_script) in remaining_commands


def test_write_hooks_document_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "hooks.json"
    path.write_text('{"hooks": {}}', encoding="utf-8")
    backup = hooks_json.write_hooks_document(path, {"hooks": {"SessionStart": []}}, backup=True)
    assert backup is not None
    assert backup.is_file()
