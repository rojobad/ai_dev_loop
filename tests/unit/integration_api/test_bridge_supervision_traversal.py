"""Unit tests for Bridge supervision traversal helpers."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from tests.support.bridge_acceptance_constants import (
    C04_FIXTURE_SEQUENCE_PHASE_EXPECTATIONS,
    EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_01,
    EXPECTED_SEQUENCE_MANIFEST_NAME,
    INITIAL_PROMPT_BYTES,
    INVALID_REVIEW_RESPONSE_BYTES,
    PROMPT_MARKER_ARTIFACT_PATHS,
    REVIEW_PROMPT_FOOTER_BYTES,
    REVIEW_RETRY_ENVELOPE_PREFIX_BYTES,
    REVIEW_SKILL_INVOCATION_BYTES,
    IndependentSequenceReportExpectation,
    assert_independent_sequence_report_content,
    expected_latest_cursor_final_response_for_review_iteration,
    verify_captured_review_prompt_bytes,
)
from tests.support.bridge_supervision_traversal import (
    _assert_review_prompt_capture,
    _assert_review_response_capture,
    _fetch_all_chunks,
)


class _RecordingClient:
    resource_requests = 0

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self._index = 0

    def invoke(self, integration_args: list[str]) -> dict[str, Any]:
        del integration_args
        self.resource_requests += 1
        page = self._pages[self._index]
        self._index += 1
        return {"data": page}


def test_fetch_all_chunks_counts_one_request_per_page() -> None:
    payload = b"abcdefghij"
    client = _RecordingClient(
        [
            {
                "available": True,
                "contentBase64": base64.b64encode(payload[:5]).decode("ascii"),
                "hasMore": True,
                "nextOffset": 5,
            },
            {
                "available": True,
                "contentBase64": base64.b64encode(payload[5:]).decode("ascii"),
                "hasMore": False,
            },
        ]
    )
    joined = _fetch_all_chunks(
        client,
        ["run", "plan", "run-1"],
        chunk_size=5,
        label="plan",
        expected_bytes=payload,
        require_multiple_chunks=True,
    )
    assert joined == payload
    assert client.resource_requests == 2


def test_fetch_all_chunks_rejects_single_page_when_multi_required() -> None:
    client = _RecordingClient(
        [
            {
                "available": True,
                "contentBase64": base64.b64encode(b"short").decode("ascii"),
                "hasMore": False,
            }
        ]
    )
    with pytest.raises(AssertionError, match="multi-chunk pagination"):
        _fetch_all_chunks(
            client,
            ["run", "plan", "run-1"],
            chunk_size=4096,
            label="plan",
            require_multiple_chunks=True,
        )


def test_fetch_all_chunks_rejects_wrong_bytes() -> None:
    client = _RecordingClient(
        [
            {
                "available": True,
                "contentBase64": base64.b64encode(b"wrong-bytes").decode("ascii"),
                "hasMore": False,
            }
        ]
    )
    with pytest.raises(AssertionError, match="byte mismatch"):
        _fetch_all_chunks(
            client,
            ["run", "plan", "run-1"],
            chunk_size=4096,
            label="plan",
            expected_bytes=b"expected-bytes",
        )


def test_fetch_all_chunks_rejects_unavailable_chunk() -> None:
    client = _RecordingClient(
        [
            {
                "available": False,
                "reason": "not_yet_produced",
            }
        ]
    )
    with pytest.raises(AssertionError, match="unavailable"):
        _fetch_all_chunks(
            client,
            ["run", "output", "run-1", "--stream", "stdout"],
            chunk_size=32,
            label="stdout",
        )


def _minimal_valid_review_prompt(*, iteration: int) -> bytes:
    iter_label = f"{iteration:02d}".encode("ascii")
    latest = expected_latest_cursor_final_response_for_review_iteration(iteration)
    return (
        b"This is an automated Codex review turn for the approved plan.\n"
        b"- Repository plan path: docs/plans/sample-plan.md\n"
        b"\n## Original Cursor prompt\n\n"
        + INITIAL_PROMPT_BYTES
        + b"\n\n## Latest Cursor final response\n\n"
        + latest
        + PROMPT_MARKER_ARTIFACT_PATHS
        + b"cursor/iterations/"
        + iter_label
        + b"/events.jsonl\n"
        + b"cursor/iterations/"
        + iter_label
        + b"/final.txt\n"
        + b"git/diffs/"
        + iter_label
        + b".patch\n"
        + REVIEW_SKILL_INVOCATION_BYTES
        + b"\n"
        + REVIEW_PROMPT_FOOTER_BYTES
    )


def test_verify_captured_review_prompt_accepts_scheduler_retry_envelope() -> None:
    retry = REVIEW_RETRY_ENVELOPE_PREFIX_BYTES + _minimal_valid_review_prompt(iteration=1)
    verify_captured_review_prompt_bytes(retry, iteration=1)


def test_verify_captured_review_prompt_accepts_fixture_shape() -> None:
    verify_captured_review_prompt_bytes(_minimal_valid_review_prompt(iteration=1), iteration=1)


def test_verify_captured_review_prompt_rejects_substituted_latest_cursor_response() -> None:
    bad = _minimal_valid_review_prompt(iteration=1).replace(
        EXPECTED_LATEST_CURSOR_FINAL_RESPONSE_ITERATION_01,
        b"substituted-latest-response",
    )
    with pytest.raises(AssertionError, match="latest Cursor final response"):
        verify_captured_review_prompt_bytes(bad, iteration=1)


def test_assert_independent_sequence_report_rejects_empty_phase_semantics() -> None:
    import json

    hollow = {
        "schema_version": 1,
        "sequence_id": "seq-controlled",
        "sequence_name": EXPECTED_SEQUENCE_MANIFEST_NAME,
        "final_outcome": "completed",
        "final_run_id": "run-final-00000000000000000000000000000001",
        "final_run_id_prefix": "run-fina",
        "residual_risk_ordinals": [],
        "residual_risk_phase_names": [],
        "base_head_sha256": "a" * 40,
        "base_head_sha256_prefix": "a" * 12,
        "final_staged_patch_sha256": "b" * 64,
        "final_staged_patch_sha256_prefix": "b" * 12,
        "phases": [
            {
                "ordinal": 1,
                "phase_name": "wrong-name",
                "run_id": "run-phase-one",
                "run_id_prefix": "run-phas",
                "accepted_outcome": "completed",
                "residual_risk": False,
                "attempt_count": 0,
                "attempt_kind_labels": [],
                "review_result_sha256": "c" * 64,
                "review_result_sha256_prefix": "c" * 12,
                "accepted_run_id_prefix": "run-phas",
            }
        ],
    }
    expectation = IndependentSequenceReportExpectation(
        sequence_id="seq-controlled",
        sequence_name=EXPECTED_SEQUENCE_MANIFEST_NAME,
        final_outcome="completed",
        final_run_id="run-final-00000000000000000000000000000001",
        run_ids_by_ordinal={1: "run-phase-one"},
        phase_expectations=(C04_FIXTURE_SEQUENCE_PHASE_EXPECTATIONS[0],),
    )
    with pytest.raises(AssertionError, match="phase 1 name"):
        assert_independent_sequence_report_content(
            json.dumps(hollow).encode("utf-8"),
            expectation=expectation,
        )


def test_verify_captured_review_prompt_rejects_wrong_embedded_cursor_prompt() -> None:
    bad = _minimal_valid_review_prompt(iteration=1).replace(
        INITIAL_PROMPT_BYTES,
        b"wrong-cursor-prompt-bytes",
    )
    with pytest.raises(AssertionError, match="embedded Cursor prompt"):
        verify_captured_review_prompt_bytes(bad, iteration=1)


def test_verify_captured_review_prompt_rejects_missing_footer() -> None:
    prompt = _minimal_valid_review_prompt(iteration=1)
    with pytest.raises(AssertionError, match="footer"):
        verify_captured_review_prompt_bytes(prompt[:-10], iteration=1)


def test_assert_review_prompt_capture_requires_available_prompt() -> None:
    class _UnreachableClient:
        resource_requests = 0

        def invoke(self, integration_args: list[str]) -> dict[str, Any]:
            raise AssertionError(f"unexpected invoke: {integration_args!r}")

    detail = {
        "iteration": 1,
        "content": {"prompt": {"available": False, "reason": "not_yet_produced"}},
    }
    with pytest.raises(AssertionError, match="unavailable"):
        _assert_review_prompt_capture(
            _UnreachableClient(),  # type: ignore[arg-type]
            run_id="run-1",
            attempt_id="att-1",
            detail_data=detail,
            required=True,
        )


def test_assert_review_response_capture_requires_expected_bytes() -> None:
    class _UnreachableClient:
        resource_requests = 0

        def invoke(self, integration_args: list[str]) -> dict[str, Any]:
            raise AssertionError(f"unexpected invoke: {integration_args!r}")

    with pytest.raises(AssertionError, match="unavailable"):
        _assert_review_response_capture(
            _UnreachableClient(),  # type: ignore[arg-type]
            run_id="run-1",
            attempt_id="att-1",
            response_flag={"available": False, "reason": "not_yet_produced"},
            expected_bytes=INVALID_REVIEW_RESPONSE_BYTES,
            forbidden=False,
        )


def test_assert_review_response_capture_forbids_available_response() -> None:
    with pytest.raises(AssertionError, match="unexpected review response availability"):
        _assert_review_response_capture(
            _RecordingClient([]),  # type: ignore[arg-type]
            run_id="run-1",
            attempt_id="att-1",
            response_flag={"available": True},
            expected_bytes=None,
            forbidden=True,
            forbidden_reasons=frozenset({"not_produced", "not_yet_produced"}),
        )
