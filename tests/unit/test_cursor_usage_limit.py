"""Unit tests for Cursor usage-limit failure classification."""

from __future__ import annotations

from ai_dev_loop.runners.cursor_failure import (
    SAFE_USAGE_LIMIT_SUMMARY,
    CursorFailureCode,
    classify_cursor_failure_text,
    is_usage_limit_text,
)

CANONICAL = (
    "ActionRequiredError: You've hit your usage limit for this model. "
    "Switch to Auto or another model to continue. "
    "Resets on 2026-08-01. Billing amount: $20."
)


def test_canonical_usage_limit_is_classified() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr=CANONICAL,
    )
    assert result.is_usage_limit
    assert result.code == CursorFailureCode.USAGE_LIMIT
    assert result.safe_summary == SAFE_USAGE_LIMIT_SUMMARY
    assert "Billing" not in (result.safe_summary or "")
    assert "2026-08-01" not in (result.safe_summary or "")


def test_case_and_whitespace_variants() -> None:
    text = (
        "  actionrequirederror : you've reached your USAGE LIMIT for the model.\n"
        "Please SWITCH TO AUTO to continue.\n"
    )
    assert is_usage_limit_text(text)


def test_missing_action_required_is_unknown() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr="You've hit your usage limit. Switch to Auto.",
    )
    assert result.code == CursorFailureCode.UNKNOWN
    assert result.safe_summary is None


def test_unrelated_action_required_is_unknown() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr="ActionRequiredError: Please approve the MCP server before continuing.",
    )
    assert result.code == CursorFailureCode.UNKNOWN


def test_generic_limit_phrase_is_unknown() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr="ActionRequiredError: rate limit exceeded for tool calls",
    )
    assert result.code == CursorFailureCode.UNKNOWN


def test_zero_exit_is_unknown_even_with_markers() -> None:
    result = classify_cursor_failure_text(
        returncode=0,
        timed_out=False,
        stderr=CANONICAL,
    )
    assert result.code == CursorFailureCode.UNKNOWN


def test_timeout_is_unknown() -> None:
    result = classify_cursor_failure_text(
        returncode=1,
        timed_out=True,
        stderr=CANONICAL,
    )
    assert result.code == CursorFailureCode.UNKNOWN


def test_nonzero_without_stderr_is_unknown() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr="",
    )
    assert result.code == CursorFailureCode.UNKNOWN


def test_structured_errors_can_classify() -> None:
    result = classify_cursor_failure_text(
        returncode=2,
        timed_out=False,
        stderr="",
        structured_errors=(CANONICAL,),
    )
    assert result.is_usage_limit


def test_usage_limit_metadata_omits_structured_billing_details() -> None:
    from ai_dev_loop.process import StreamingProcessResult
    from ai_dev_loop.runners.cursor import (
        CursorExecutionResult,
        CursorParseResult,
        cursor_metadata_errors,
    )

    process = StreamingProcessResult(
        args=["agent", "-p", "prompt"],
        returncode=2,
        stdout='{"type":"error","message":"' + CANONICAL.replace('"', '\\"') + '"}',
        stderr="",
        timed_out=False,
        elapsed_seconds=1.0,
    )
    parse = CursorParseResult(
        final_text=None,
        errors=(CANONICAL,),
        parse_ok=False,
    )
    failure = classify_cursor_failure_text(
        returncode=process.returncode,
        timed_out=process.timed_out,
        stderr=process.stderr,
        structured_errors=parse.errors,
    )
    execution = CursorExecutionResult(
        process=process,
        parse=parse,
        metadata_args=["agent", "-p", "prompt"],
        failure=failure,
    )
    assert execution.failure.is_usage_limit
    metadata_errors = cursor_metadata_errors(execution)
    assert metadata_errors == []
    assert "Billing" not in str(metadata_errors)
    assert "2026-08-01" not in str(metadata_errors)
