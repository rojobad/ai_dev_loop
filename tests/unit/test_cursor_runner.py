"""Unit tests for Cursor runner helpers."""

from __future__ import annotations

from ai_dev_loop.runners.cursor import (
    build_cursor_args,
    parse_stream_json,
    redact_cursor_args,
)
from ai_dev_loop.state import CursorState


def test_build_cursor_args_includes_prompt_as_last_argument() -> None:
    cursor = CursorState(
        command="agent",
        model="composer-2.5-fast",
        output_format="stream-json",
        force=True,
        trust_workspace=True,
        sandbox="disabled",
    )
    args = build_cursor_args(
        cursor,
        repo_root="/tmp/repo",
        chat_id="019abc00-1111-2222-3333-444444444444",
        prompt="Implement the plan exactly.",
    )
    assert args[0] == "agent"
    assert "-p" in args
    assert "--force" in args
    assert "--trust" in args
    assert args[-1] == "Implement the plan exactly."
    assert args[args.index("--resume") + 1] == "019abc00-1111-2222-3333-444444444444"


def test_redact_cursor_args_hides_prompt() -> None:
    args = ["agent", "-p", "secret prompt"]
    assert redact_cursor_args(args) == ["agent", "-p", "<prompt-redacted>"]


def test_parse_stream_json_extracts_result_and_errors() -> None:
    stdout = "\n".join(
        [
            '{"type":"assistant","text":"working"}',
            '{"type":"error","message":"tool failed"}',
            '{"type":"result","result":"final answer"}',
        ]
    )
    parsed = parse_stream_json(stdout)
    assert parsed.final_text == "final answer"
    assert parsed.errors == ("tool failed",)
    assert parsed.parse_ok is False


def test_parse_stream_json_marks_invalid_json_as_parse_failure() -> None:
    parsed = parse_stream_json("not-json\n")
    assert parsed.final_text is None
    assert parsed.parse_ok is False
